/**
 * 表格知识制品 API（61号 §十）—— 经 main_control 反向代理到 mining 服务。
 *
 * 走 createProxyClient('mining')：baseURL 每次请求按当前域重算
 * （/api/control-plane/api/v1/proxy/{domain}/mining）。制品路由本身不消费
 * domain 查询参数（域过滤走显式 domain_id），故关掉自动 domain 追加。
 * 错误文案统一由调用方用 apiErrorDetail 提取（409 带 {code} 信封）。
 */
import { createProxyClient } from '@/api/proxyClient'
import type {
  Artifact,
  ArtifactColumn,
  ArtifactDetail,
  ArtifactKind,
  ArtifactRole,
  ArtifactRowsPage,
  DiffResult,
  ImportBatch,
  ImportConflictPolicy,
  ImportReport,
  MemberInfo,
  PreviewEnvelope,
  PublishResult,
  RevisionInfo,
  WikiLink,
  WikiPage,
  WikiPageMeta,
  WikiPageType,
  WikiStatus,
} from '@/types/artifacts'

export interface ArtifactCreateRequest {
  title: string
  description?: string
  domain_id?: string
  visibility?: string
  /** wiki 制品（60号四形态）：创建时声明，缺省 table（后端 Literal 默认）。 */
  kind?: ArtifactKind
}

export interface ArtifactMetaPatchRequest {
  title?: string
  description?: string
  usage_note?: string
  /** 008：owner 专属（后端非 owner 403）。 */
  visibility?: string
  expected_updated_at?: string
}

export interface ColumnAddRequest {
  column_name: string
  column_type: string
  enum_values?: string[]
  is_indexed?: boolean
  expected_updated_at?: string
}

export interface ColumnPatchRequest {
  column_name?: string
  column_type?: string
  enum_values?: string[]
  is_indexed?: boolean
  status?: 'dropped'
  expected_updated_at?: string
}

export interface BatchAssignRequest {
  column: string
  value: unknown
  /** is_null / not_null 条件不带 value。 */
  where: Array<{ column: string; op: string; value?: unknown }>
  expected_updated_at?: string
}

export interface ImportRequest {
  preview_token: string
  /** build_mapping 产物，或 {sheet, header_row, column_ops} 原始 ops（后端补跑映射）。 */
  plan: Record<string, unknown>
  conflict_policy?: ImportConflictPolicy
}

/** PUT /wiki/page/{hex} 请求体（63号 §5.5 WikiPagePut 同构）。 */
export interface WikiPagePutRequest {
  title: string
  page_type?: WikiPageType
  frontmatter?: Record<string, unknown>
  body_md?: string
  /** 显式键：编辑流稳定寻址（title 改名不漂移）/改名流；缺省=后端按 title 归一。 */
  page_key?: string
  provenance?: unknown[] | null
  flags?: Record<string, unknown> | null
  ordinal?: number | null
  expected_updated_at?: string
}

/**
 * page_key 的 URL 传输编码（wiki 网关铁律）：raw path 含 % 即被网关 400，
 * 故中文键一律 UTF-8 字节 → 纯 hex（与后端 decode_page_key 对齐）。
 * 纯函数（模块级导出）——组件与 API 客户端共用。
 */
export function encodePageKey(pageKey: string): string {
  const bytes = new TextEncoder().encode(pageKey)
  let hex = ''
  for (const byte of bytes) hex += byte.toString(16).padStart(2, '0')
  return hex
}

/** update_cell 返回 {row_key, data, conflicts?}（成功格回显）。 */
export interface ArtifactRowLike {
  row_key: string
  data: Record<string, unknown>
  conflicts?: Record<string, unknown> | null
}

export function useArtifactsApi() {
  const client = createProxyClient('mining', { includeDomainQuery: false })

  return {
    // ── 制品 CRUD ──
    async create(payload: ArtifactCreateRequest): Promise<Artifact> {
      const { data } = await client.post('/api/artifacts', payload)
      return data
    },

    async list(params?: { domain_id?: string }): Promise<Artifact[]> {
      const { data } = await client.get('/api/artifacts', { params })
      return data.artifacts ?? []
    },

    async get(artifactId: string): Promise<ArtifactDetail> {
      const { data } = await client.get(`/api/artifacts/${artifactId}`)
      return data
    },

    async updateMeta(
      artifactId: string, payload: ArtifactMetaPatchRequest,
    ): Promise<Artifact> {
      const { data } = await client.patch(`/api/artifacts/${artifactId}`, payload)
      return data
    },

    async remove(artifactId: string): Promise<void> {
      await client.delete(`/api/artifacts/${artifactId}`)
    },

    // ── 生命周期 ──
    async publish(
      artifactId: string,
      payload: { change_summary: string; expected_updated_at?: string },
    ): Promise<PublishResult> {
      const { data } = await client.post(`/api/artifacts/${artifactId}/publish`, payload)
      return data
    },

    async rollback(
      artifactId: string, revision: number, expectedUpdatedAt?: string,
    ): Promise<Artifact> {
      const { data } = await client.post(
        `/api/artifacts/${artifactId}/rollback/${revision}`,
        { expected_updated_at: expectedUpdatedAt },
      )
      return data
    },

    async deprecate(artifactId: string, expectedUpdatedAt?: string): Promise<Artifact> {
      const { data } = await client.post(
        `/api/artifacts/${artifactId}/deprecate`,
        { expected_updated_at: expectedUpdatedAt },
      )
      return data
    },

    async republish(artifactId: string, expectedUpdatedAt?: string): Promise<Artifact> {
      const { data } = await client.post(
        `/api/artifacts/${artifactId}/republish`,
        { expected_updated_at: expectedUpdatedAt },
      )
      return data
    },

    async discardDraft(artifactId: string, expectedUpdatedAt?: string): Promise<Artifact> {
      const { data } = await client.post(
        `/api/artifacts/${artifactId}/discard`,
        { expected_updated_at: expectedUpdatedAt },
      )
      return data
    },

    /** draft vs current 差异（发布确认页数字口径）。 */
    async diff(artifactId: string): Promise<DiffResult> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/diff`)
      return data
    },

    /** 版本历史（运行态信息，viewer+ 可读；revision 倒序）。 */
    async listRevisions(artifactId: string): Promise<RevisionInfo[]> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/revisions`)
      return data.revisions ?? []
    },

    // ── 成员 ──
    async listMembers(artifactId: string): Promise<MemberInfo[]> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/members`)
      return data.members ?? []
    },

    async addMember(
      artifactId: string, userId: string, role: ArtifactRole,
    ): Promise<MemberInfo> {
      const { data } = await client.post(
        `/api/artifacts/${artifactId}/members`, { user_id: userId, role })
      return data
    },

    async updateMember(
      artifactId: string, userId: string, role: ArtifactRole,
    ): Promise<MemberInfo> {
      const { data } = await client.patch(
        `/api/artifacts/${artifactId}/members/${userId}`, { role })
      return data
    },

    async removeMember(artifactId: string, userId: string): Promise<void> {
      await client.delete(`/api/artifacts/${artifactId}/members/${userId}`)
    },

    // ── 导入向导（§六 三步）──
    async uploadTable(artifactId: string, file: File): Promise<PreviewEnvelope> {
      const form = new FormData()
      form.append('file', file)
      const { data } = await client.post(`/api/artifacts/${artifactId}/table/upload`, form)
      return data
    },

    async getPreview(artifactId: string, token: string): Promise<PreviewEnvelope> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/table/preview/${token}`)
      return data
    },

    async importTable(artifactId: string, payload: ImportRequest): Promise<ImportReport> {
      const { data } = await client.post(`/api/artifacts/${artifactId}/table/import`, payload)
      return data
    },

    async listBatches(artifactId: string): Promise<ImportBatch[]> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/batches`)
      return data.batches ?? []
    },

    // ── 列定义 ──
    /** options.includeDropped=含软删列（设计态）；options.runtime=发布定格
     * 快照列（revisions.schema_json，viewer+ 可读——运行态表头口径）。 */
    async listColumns(
      artifactId: string, options?: { includeDropped?: boolean; runtime?: boolean },
    ): Promise<ArtifactColumn[]> {
      const params: Record<string, unknown> = {}
      if (options?.includeDropped) params.include_dropped = true
      if (options?.runtime) params.runtime = true
      const { data } = await client.get(`/api/artifacts/${artifactId}/table/columns`, {
        params: Object.keys(params).length > 0 ? params : undefined,
      })
      return data.columns ?? []
    },

    async addColumn(
      artifactId: string, payload: ColumnAddRequest,
    ): Promise<ArtifactColumn> {
      const { data } = await client.post(`/api/artifacts/${artifactId}/table/columns`, payload)
      return data
    },

    async updateColumn(
      artifactId: string, columnId: string, payload: ColumnPatchRequest,
    ): Promise<ArtifactColumn> {
      const { data } = await client.patch(
        `/api/artifacts/${artifactId}/table/columns/${columnId}`, payload)
      return data
    },

    async setPrimaryKey(
      artifactId: string, columnIds: string[], expectedUpdatedAt?: string,
    ): Promise<Artifact> {
      const { data } = await client.post(
        `/api/artifacts/${artifactId}/table/primary-key`,
        { column_ids: columnIds, expected_updated_at: expectedUpdatedAt },
      )
      return data
    },

    // ── 行 ──
    async listRows(
      artifactId: string, params?: { draft?: boolean; page?: number; size?: number },
    ): Promise<ArtifactRowsPage> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/table/rows`, { params })
      return data
    },

    async updateCell(
      artifactId: string, rowKey: string,
      payload: { column: string; value: unknown; expected_updated_at?: string },
    ): Promise<ArtifactRowLike> {
      const { data } = await client.patch(
        `/api/artifacts/${artifactId}/table/rows/${encodeURIComponent(rowKey)}`, payload)
      return data
    },

    /** 手工行增（61号 §7.2）：返回新行（含 row_key/ordinal/provenance）。 */
    async insertRow(
      artifactId: string,
      payload: { data: Record<string, unknown>; expected_updated_at?: string },
    ): Promise<ArtifactRowLike & { ordinal: number; provenance: unknown }> {
      const { data } = await client.post(
        `/api/artifacts/${artifactId}/table/rows`, payload)
      return data
    },

    async batchAssign(
      artifactId: string, payload: BatchAssignRequest,
    ): Promise<{ updated: number }> {
      const { data } = await client.post(
        `/api/artifacts/${artifactId}/table/rows/batch-assign`, payload)
      return data
    },

    async deleteRow(
      artifactId: string, rowKey: string, expectedUpdatedAt?: string,
    ): Promise<void> {
      await client.delete(
        `/api/artifacts/${artifactId}/table/rows/${encodeURIComponent(rowKey)}`,
        { data: { expected_updated_at: expectedUpdatedAt } },
      )
    },

    // ── Wiki 制品（63号批次2 六端点）──
    /** 页面清单：设计态编辑传 draft=true（rev0 工作区；干净态投影 current）；
     * viewer 走 runtime 视图（draft=false 读 current_revision）。 */
    async listWikiPages(
      artifactId: string, params?: { draft?: boolean; revision?: number },
    ): Promise<WikiPageMeta[]> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/wiki/pages`, { params })
      return data.pages ?? []
    },

    /** 单页全字段（page_key 自动 hex 编码进 URL——raw path 含 % 即网关 400）。 */
    async getWikiPage(
      artifactId: string, pageKey: string,
      params?: { draft?: boolean; revision?: number },
    ): Promise<WikiPage> {
      const { data } = await client.get(
        `/api/artifacts/${artifactId}/wiki/page/${encodePageKey(pageKey)}`, { params })
      return data
    },

    /** 单页创建/更新：addressKey=URL 寻址键（新建=title 原文；编辑=原 page_key）。
     * 返回后端归一定稿的完整页——前端按返回 page_key 刷新选中。 */
    async upsertWikiPage(
      artifactId: string, addressKey: string, payload: WikiPagePutRequest,
    ): Promise<WikiPage> {
      const { data } = await client.put(
        `/api/artifacts/${artifactId}/wiki/page/${encodePageKey(addressKey)}`, payload)
      return data
    },

    async deleteWikiPage(
      artifactId: string, pageKey: string, expectedUpdatedAt?: string,
    ): Promise<void> {
      await client.delete(
        `/api/artifacts/${artifactId}/wiki/page/${encodePageKey(pageKey)}`,
        { data: { expected_updated_at: expectedUpdatedAt } },
      )
    },

    /** 出入边（63号 §2.4 反向链接）：page_key 是明文 query 参数——
     * query 不经网关 path 校验，无需 hex。 */
    async listWikiLinks(
      artifactId: string,
      params?: { page_key?: string; direction?: 'out' | 'in'; draft?: boolean },
    ): Promise<WikiLink[]> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/wiki/links`, { params })
      return data.links ?? []
    },

    /** units embedding 进度（运行态信息，viewer+ 可读）。 */
    async getWikiStatus(artifactId: string): Promise<WikiStatus> {
      const { data } = await client.get(`/api/artifacts/${artifactId}/wiki/status`)
      return data
    },
  }
}
