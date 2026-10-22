/**
 * 表格知识制品（61号 §十 REST 契约）。
 *
 * 错误体：`{"detail":{"code":...}}`（stale_artifact / primary_key_conflict）或
 * FastAPI 默认字符串 detail——统一走 apiErrorDetail 取文案。
 */

export type ArtifactKind = 'table' | 'wiki'
export type ArtifactStatus = 'draft' | 'published' | 'deprecated'
export type ArtifactRole = 'owner' | 'editor' | 'viewer'
/** 008 可见性：private=仅成员；public=本域全体绑定用户可读（写仍走成员角色）。 */
export type ArtifactVisibility = 'private' | 'public'
/** 与后端 _VALID_COLUMN_TYPES 对齐（build_mapping / 索引 cast 同口径）。 */
export type ArtifactColumnType = 'text' | 'enum' | 'int' | 'float' | 'date' | 'bool'

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
  visibility?: ArtifactVisibility | string
  current_revision: number
  draft_open: boolean
  created_at: string
  updated_at: string
  /** 列表与详情口径附带（get_artifact 附 my_role；admin 归一 owner）。 */
  my_role?: ArtifactRole | null
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
  /** 后端 list_members JOIN 回填（user_id 是内部 id，展示用登录名/显示名）。 */
  username?: string | null
  user_name?: string | null
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

/** 单个 sheet 的预览（codex二审功能项①：每 sheet 独立表头/推断/样例）。 */
export interface SheetPreview {
  name: string
  columns: PreviewColumn[]
  sample_rows: Array<Record<string, unknown>>
}

export interface PreviewPayload {
  file_name: string
  /** per-sheet 对象（后端多 sheet 扫描）；default_sheet = sheets[0].name。 */
  sheets: SheetPreview[]
  default_sheet: string
  /** 默认 sheet 的引用（向后兼容顶层口径）。 */
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

/** import 执行报告（execute_import 返回体）。 */
export interface ImportReport {
  artifact_id?: string
  mode?: 'create' | 'append' | string
  batch_id?: string
  sheet?: string
  row_count?: number
  inserted?: number
  updated?: number
  type_errors?: number
  key_conflicts?: number
  /** 冲突样例（≤20 条）：{row_index, row_key, column, message}。 */
  conflicts_sample?: Array<Record<string, unknown>>
  /** 追加模式 schema 有文件无（该批置 null）的列。 */
  missing_columns?: Array<{ column_id: string; column_name: string }>
  [key: string]: unknown
}

// ── Wiki 制品（63号 §2 对象层 / §4.3 页型）──

/** 页面类型全集（63号 §4.3，与后端 PAGE_TYPES 对齐）。 */
export type WikiPageType = 'topic' | 'glossary' | 'contradiction' | 'open_question'
/** 页面来源（63号 §3.2）：machine=编译管线产物；manual=人工编辑。 */
export type WikiPageOrigin = 'machine' | 'manual'

/** 页面轻量清单行（GET /wiki/pages 信封 {pages}；不含 body_md/frontmatter，
 * aliases 自 frontmatter.aliases 派生为 list[str]——阅读器链接解析兜底用）。 */
export interface WikiPageMeta {
  page_key: string
  title: string
  page_type: WikiPageType
  origin: WikiPageOrigin
  ordinal: number
  flags: Record<string, unknown> | null
  aliases: string[]
  updated_at: string
  updated_by: string
}

/** 单页全字段（GET/PUT /wiki/page/{hex} 同形——PUT 返回 get_page 结果，
 * page_key 为后端归一定稿键）。 */
export interface WikiPage extends WikiPageMeta {
  frontmatter: Record<string, unknown>
  body_md: string
  provenance?: unknown[] | null
}

/** 页面边（GET /wiki/links 信封 {links}；to_page 已按 page_key ∪ aliases
 * 归一到 canonical 键——反向链接与阅读器跳转的正确性前提）。 */
export interface WikiLink {
  from_page: string
  to_page: string
  anchor_text: string
  status: 'valid' | 'broken'
}

/** units embedding 进度四计数（GET /wiki/status 的 units 面）。 */
export interface WikiUnitsStatus {
  total: number
  done: number
  failed: number
  pending: number
}

/** GET /wiki/status 返回体：{revision, units 四计数}。 */
export interface WikiStatus {
  revision: number
  units: WikiUnitsStatus
}

// ── Wiki 编译管线（63号批次3：sources 三端点 + compile 四端点）──

/**
 * 绑定源行（GET /wiki/sources 信封 {sources}）。dirty 三态（63号批次3
 * 裁定）：true=绑定范围内语料已变化（建议重编译）；false=与指纹一致；
 * null=库已不可读，无法判定（前端显示「未知」，行仍可见只隐库元数据）。
 * dirty/kb_title/unreadable 为列表端点计算字段——PUT 响应不带（可选）。
 */
export interface WikiSource {
  kb_id: string
  /** 决策6 契约：{"directories": ["a/b", ...]}（空数组=全库）。 */
  scope: { directories?: string[] } & Record<string, unknown>
  status: string | null
  fingerprint: Record<string, unknown> | null
  created_by?: string | null
  created_at: string
  dirty?: boolean | null
  kb_title?: string | null
  unreadable?: boolean
}

/** 目录页提示（toc 树叶子；POST /wiki/compile/toc 请求体同构）。 */
export interface WikiTocPageHint {
  title: string
  cluster_ids: string[]
  page_type: string
}

/** 目录节（awaiting_toc 时 status.toc 数组元素 / 确认请求体元素）。 */
export interface WikiTocSection {
  section_title: string
  page_hints: WikiTocPageHint[]
}

/** 编译状态机全集（批次3 决策1；无批时后端 404 compile_not_found）。 */
export type WikiCompileState =
  | 'preparing' | 'awaiting_toc' | 'writing'
  | 'completed' | 'failed' | 'cancelled'

/** 编译报告计数（prepare 与 writing 两段合并；缺项由端点补 0/省略）。 */
export interface WikiCompileStats {
  /** prepare 段：取材单元数 / 聚类数 / 向量覆盖率。 */
  unit_count?: number
  cluster_count?: number
  embedded_ratio?: number
  /** writing 段。 */
  pages_written?: number
  failed_pages?: number
  dropped_pages?: number
  broken_links?: number
  dangling_pages?: number
  /** 专页计数（质量调用 + 术语表派生）。 */
  contradiction_pages?: number
  open_question_pages?: number
  glossary_pages?: number
  /** 机器页与人工页同键冲突数（conflicts 明细的长度口径）。 */
  conflicts_n?: number
  [key: string]: unknown
}

/** origin 冲突明细行（机器页撞 manual 键被跳过——人工裁决后重编译）。 */
export interface WikiCompileConflict {
  page_key?: string
  manual_title?: string
  machine_title?: string
  [key: string]: unknown
}

/**
 * GET /wiki/compile/status 返回体（design-only；无批 → 404
 * compile_not_found，前端映射 idle）。toc 仅 awaiting_toc 时附带。
 */
export interface WikiCompileStatus {
  batch_id: string
  state: WikiCompileState | string
  progress?: { batch?: string; pages_done?: number }
  stats?: WikiCompileStats
  conflicts?: WikiCompileConflict[]
  toc?: WikiTocSection[]
  error?: string | null
}
